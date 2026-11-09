"""Slice-4 schedule + PRIO tests, extended by cron-folded-into-recurring (folding the retired
``kind='cron'`` onto the recurring facet — ``meta.schedule`` presence).

Layers:

* the cron parser + ``every:`` shorthand translator;
* the one-shot ``at``/``catch_up`` schedule shape + ``one_shot_action``;
* the Watches umbrella seed (idempotent on ``meta.builtin``);
* the per-tick spawn loop (idempotency stamp, collision-skip,
  backfill on/off);
* the push-delivery tick path (``meta.deliver``) and the
  one-shot resolve-and-retire path;
* the PRIO column wiring (``put(prio=N)``, ``tag(prio=N)``; the
  ``PRIO:*`` tag is a column alias — translated + stripped on write,
  the shared ``_prio_tag`` semantics);
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
    one_shot_action,
    run_schedule_pass,
    ticks_since,
    validate_schedule,
)
from precis.workers.schedule.parse import every_to_cron, parse_cron
from precis.workers.schedule.worker import (
    _has_open_previous_tick_conn,
    sweep_settled_tick_instances,
)


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
    with pytest.raises(BadInput, match="more than one of 'cron'/'every'/'at'"):
        validate_schedule({"cron": "0 * * * *", "every": "1h"})


def test_validate_schedule_rejects_unknown_key() -> None:
    with pytest.raises(BadInput, match="unknown meta.schedule keys"):
        validate_schedule({"cron": "0 * * * *", "frob": True})


def test_validate_schedule_rejects_bad_backfill_type() -> None:
    with pytest.raises(BadInput, match="backfill_missed must be a bool"):
        validate_schedule({"cron": "0 * * * *", "backfill_missed": "yes"})


# ── validator: one-shot `at` shape ───────────────────────


def test_validate_schedule_accepts_at_shape() -> None:
    s = validate_schedule({"at": "2026-06-12T09:00:00Z"})
    assert s.cron is None
    assert s.at == "2026-06-12T09:00:00+00:00"
    assert s.catch_up is True  # default


def test_validate_schedule_at_catch_up_false() -> None:
    s = validate_schedule({"at": "2026-06-12T09:00:00Z", "catch_up": False})
    assert s.catch_up is False


def test_validate_schedule_rejects_at_with_cron() -> None:
    with pytest.raises(BadInput, match="more than one of 'cron'/'every'/'at'"):
        validate_schedule({"at": "2026-06-12T09:00:00Z", "cron": "0 9 * * 1"})


def test_validate_schedule_rejects_backfill_missed_with_at() -> None:
    with pytest.raises(BadInput, match="not accepted alongside 'at'"):
        validate_schedule({"at": "2026-06-12T09:00:00Z", "backfill_missed": True})


def test_validate_schedule_rejects_catch_up_with_cron() -> None:
    with pytest.raises(BadInput, match="not accepted alongside 'cron'/'every'"):
        validate_schedule({"cron": "0 9 * * 1", "catch_up": True})


def test_validate_schedule_rejects_bad_at_timestamp() -> None:
    with pytest.raises(BadInput, match="unparseable meta.schedule.at"):
        validate_schedule({"at": "not-a-date"})


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


# ── one_shot_action ───────────────────────────────────────


def test_one_shot_action_waits_before_due() -> None:
    at = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    now = datetime(2026, 6, 14, 11, 0, tzinfo=UTC)
    assert one_shot_action(at, catch_up=False, now=now) == "wait"


def test_one_shot_action_fires_on_time() -> None:
    at = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    assert one_shot_action(at, catch_up=False, now=at) == "fire"


def test_one_shot_action_fires_within_grace_even_without_catch_up() -> None:
    at = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    now = at + timedelta(seconds=30)
    assert one_shot_action(at, catch_up=False, now=now) == "fire"


def test_one_shot_action_expires_past_grace_without_catch_up() -> None:
    at = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    now = at + timedelta(hours=3)
    assert one_shot_action(at, catch_up=False, now=now) == "expire"


def test_one_shot_action_catch_up_fires_however_overdue() -> None:
    at = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    now = at + timedelta(days=3)
    assert one_shot_action(at, catch_up=True, now=now) == "fire"


# ── Watches umbrella seed (DB) ─────────────────────────────────────


def test_ensure_watches_root_is_idempotent(store: Store) -> None:
    a = ensure_watches_root(store)
    b = ensure_watches_root(store)
    assert a == b
    ref = store.get_ref(kind="todo", id=a)
    assert ref is not None
    assert ref.meta.get("builtin") == WATCHES_BUILTIN
    # The umbrella is a folder (no meta.schedule) — no facet fields either.
    assert ref.meta.get("schedule") is None


def test_watches_root_delete_is_refused(handler: TodoHandler, store: Store) -> None:
    rid = ensure_watches_root(store)
    with pytest.raises(BadInput, match="builtin"):
        handler.delete(id=rid)


# ── put: recurring defaults under Watches ──────────────────────────


def test_put_level_recurring_defaults_parent_to_watches(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Check arxiv weekly",
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
            meta={"schedule": {"cron": "0 9 * *"}},
        )


# ── put: one-shot `at` schedule + `meta.deliver` ─────────


def test_put_one_shot_at_schedule_is_canonicalised(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="remind me",
        meta={"schedule": {"at": "2026-06-12T09:00:00Z"}},
    )
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta["schedule"] == {
        "at": "2026-06-12T09:00:00+00:00",
        "catch_up": True,
    }


def test_put_with_deliver_target_is_canonicalised(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="check the api monitor",
        meta={
            "schedule": {"every": "15m"},
            "deliver": {"target": "  conv:discord/g/c/t  "},
        },
    )
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta["deliver"] == {"target": "conv:discord/g/c/t"}


def test_put_with_bad_deliver_rejected_at_write_time(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="meta.deliver.target is required"):
        handler.put(
            text="bad",
            meta={"schedule": {"every": "1h"}, "deliver": {}},
        )


# ── PRIO column ────────────────────────────────────────────────────


def test_put_with_prio_writes_column(handler: TodoHandler, store: Store) -> None:
    resp = handler.put(text="prio task", prio=1)
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 1


def test_put_rejects_out_of_range_prio(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="out of range"):
        handler.put(text="bad", prio=11)
    with pytest.raises(BadInput, match="out of range"):
        handler.put(text="bad", prio=0)


def test_put_with_prio_tag_translates_to_column(
    handler: TodoHandler, store: Store
) -> None:
    """``PRIO:*`` is an alias for the ``prio`` column — translated on write
    and stripped, the gripe/quest semantics (``_prio_tag``). The 2026-07
    de-aliasing ("canonical is put(prio=N)") left todo with NO working
    priority surface over MCP, because ``prio=`` was never declared on the
    verb — restored alongside wiring the kwarg through tools/core.py."""
    resp = handler.put(text="urgent", tags=["PRIO:urgent"])
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 1
    tags = {str(t) for t in store.tags_for(rid)}
    assert "PRIO:urgent" not in tags  # stripped — priority lives in one place


def test_numeric_prio_tag_is_the_column_spelling(
    handler: TodoHandler, store: Store
) -> None:
    """gr592664: the ``PRIO:`` tag form carries the 1..10 column number —
    one axis, two spellings (``PRIO:3`` is ``prio=3``, ``PRIO:2`` needs no
    name). prod had ``put(kind='todo', tags=['PRIO:3'])`` refused."""
    resp = handler.put(text="numbered", tags=["PRIO:3"])
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 3
    tags = {str(t) for t in store.tags_for(rid)}
    assert not any(t.startswith("PRIO:") for t in tags)  # stripped like names

    # tag() takes the number too, and any recognised PRIO:* spelling in
    # remove clears the column — same semantics as the named ladder.
    handler.tag(id=rid, add=["PRIO:10"])
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 10
    handler.tag(id=rid, remove=["PRIO:10"])
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio is None


def test_numeric_prio_tag_off_scale_or_p_prefixed_still_refuses(
    handler: TodoHandler,
) -> None:
    """``PRIO:0`` / ``PRIO:11`` name no column value and ``PRIO:p2`` reads
    as the P0..P4 scale, not the column — still no single reading."""
    for bad in ("PRIO:0", "PRIO:11", "PRIO:p2"):
        with pytest.raises(BadInput, match="invalid PRIO value"):
            handler.put(text="bad", tags=[bad])


def test_parse_strict_numeric_prio_canonicalises() -> None:
    """``PRIO:03`` parses to the canonical ``PRIO:3``; ``PRIO:p3`` and
    out-of-range numbers stay refused at the tag-vocabulary layer (filter
    paths go through parse_strict, not split_prio)."""
    assert str(Tag.parse_strict("PRIO:3")) == "PRIO:3"
    assert str(Tag.parse_strict("PRIO:03")) == "PRIO:3"
    for bad in ("PRIO:0", "PRIO:11", "PRIO:p3"):
        with pytest.raises(BadInput, match="invalid PRIO value"):
            Tag.parse_strict(bad)


def test_prio_renders_the_ladder_name(handler: TodoHandler) -> None:
    """gr592664: the read spells the same axis the write took — the
    stored number with its ladder name (``prio: 3 (high)``); off-ladder
    numbers (2) have no name and render bare. Storage stays numeric."""
    resp = handler.put(text="hot", prio=3)
    assert "prio=3 (high)" in resp.body  # the create ack echoes the write
    rid = _id_of(resp.body)
    assert "prio: 3 (high)" in handler.get(id=rid).body

    rid2 = _id_of(handler.put(text="mid", prio=2).body)
    card = handler.get(id=rid2).body
    assert "prio: 2" in card
    assert "prio: 2 (" not in card

    # So does the batch summary block.
    batch = handler.get(id=[rid, rid2]).body
    assert "prio=3 (high)" in batch
    assert "prio=2" in batch

    # A bare prio-write receipt names it too.
    resp = handler.tag(id=rid2, prio=8)
    assert "prio=8 (low)" in resp.body

    # The fisheye focal card renders the same spelling.
    eye = handler.get(id=rid, view="fisheye").body
    assert "prio: 3 (high)" in eye


def test_tag_prio_kwarg_writes_column(handler: TodoHandler, store: Store) -> None:
    resp = handler.put(text="x")
    rid = _id_of(resp.body)
    handler.tag(id=rid, prio=3)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 3


def test_tag_remove_prio_tag_clears_column(handler: TodoHandler, store: Store) -> None:
    """Removing a ``PRIO:*`` alias clears the ``prio`` column back to the
    sort-time default — the gripe/quest semantics, shared by todo again
    (see test_put_with_prio_tag_translates_to_column)."""
    resp = handler.put(text="y", prio=8)
    rid = _id_of(resp.body)
    handler.tag(id=rid, add=["PRIO:low"])
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 8  # PRIO:low → 8
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


def test_schedule_pass_spawns_one_child(handler: TodoHandler, store: Store) -> None:
    # Daily at 00:00, no backfill, last tick 25h ago → mints today's.
    resp = handler.put(
        text="Daily",
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
            "FROM refs WHERE parent_id = %s AND retired_at IS NULL",
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
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rid = _id_of(resp.body)
    yesterday_tick = datetime.now(UTC) - timedelta(hours=25)
    _set_last_tick(store, rid, yesterday_tick)
    run_schedule_pass(store, limit=50)
    run_schedule_pass(store, limit=50)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL",
            (rid,),
        ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 1


def test_schedule_pass_skips_when_previous_still_open(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Hourly",
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


def test_schedule_pass_failed_previous_tick_does_not_wedge(
    handler: TodoHandler, store: Store
) -> None:
    """A previous tick whose job *failed* must not block the next tick.

    Regression for the news/briefing wedge: a ``news_poll`` / ``briefing``
    tick that fails bubbles a ``child-failed:<job_id>`` open tag onto the
    spawned child but leaves it ``STATUS:open``. The collision-skip used to
    treat that as "previous still open" and refuse to ever mint another
    tick — silently freezing the whole recurring. A failed tick is
    terminal-for-scheduling, so the next tick must still fire.
    """
    resp = handler.put(
        text="Hourly",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)

    # Hand-build a *previous* spawned tick that failed: STATUS:open +
    # a ``child-failed:*`` bubble, with an old ``spawned_for_tick`` stamp
    # so it can't collide with the tick this pass will mint.
    old_stamp = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="minutes")
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="Hourly (failed prior tick)",
        meta={"spawned_for_tick": old_stamp},
        parent_id=rid,
        prio=2,
    )
    store.add_tag(child.id, Tag.closed("STATUS", "open"), set_by="system")
    store.add_tag(child.id, Tag.open("child-failed:999"), set_by="system")

    # Make one tick due and run: it must mint despite the failed-but-open
    # previous child (which used to wedge the recurring forever).
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=1, minutes=5))
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1, "failed previous tick should not block the next one"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL "
            "AND meta->>'spawned_for_tick' <> %s",
            (rid, old_stamp),
        ).fetchone()
    assert row is not None
    n = int(row[0])
    assert n == 1, "a fresh tick should have been minted"


def test_schedule_pass_copies_params_verbatim_onto_child(
    handler: TodoHandler, store: Store
) -> None:
    """``meta.params`` rides verbatim onto every spawned tick child —
    the seam a fenced tick's ``params.envelope`` (td573740) relies on:
    a scout recurring declares its box once on the root and every tick
    it mints carries the pin."""
    params = {
        "model": "haiku",
        "envelope": "scout",
        "scout": {"folder": 1, "quest": 2},
    }
    resp = handler.put(
        text="Scout watch",
        meta={"schedule": {"cron": "0 * * * *"}, "params": params},
    )
    rid = _id_of(resp.body)
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=1, minutes=5))

    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'params' FROM refs "
            "WHERE parent_id = %s AND retired_at IS NULL",
            (rid,),
        ).fetchone()
    assert row is not None
    assert row[0] == params


def _spawn_open_child(
    store: Store, rid: int, *, status: str, created_at: datetime
) -> int:
    """Hand-mint a spawned tick-child under ``rid`` with a given STATUS and
    ``created_at`` (mint time), for :func:`_has_open_previous_tick_conn`
    unit tests. Returns the child's ref id."""
    stamp = created_at.isoformat(timespec="minutes")
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="Hourly (tick child)",
        meta={"spawned_for_tick": stamp},
        parent_id=rid,
        prio=2,
    )
    store.add_tag(child.id, Tag.closed("STATUS", status), set_by="system")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (created_at, child.id),
        )
        conn.commit()
    return child.id


def test_has_open_previous_tick_conn_ignores_zombie_older_than_stuck_hours(
    handler: TodoHandler, store: Store
) -> None:
    """A still-``running`` tick-child older than ``stuck_hours`` is a
    zombie (orphaned by a killed compose, lease-churn defeats the
    sweeper) — it must NOT block the cadence."""
    resp = handler.put(
        text="Hourly",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    old_created = datetime.now(UTC) - timedelta(hours=3)
    _spawn_open_child(store, rid, status="running", created_at=old_created)

    with store.pool.connection() as conn:
        blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
    assert blocked is False, "a zombie tick-child older than stuck_hours must not block"


def test_has_open_previous_tick_conn_blocks_recent_open_child(
    handler: TodoHandler, store: Store
) -> None:
    """A genuinely in-flight child spawned within ``stuck_hours`` still
    blocks — prevents pile-up on a frequent cadence (e.g. news_poll)."""
    resp = handler.put(
        text="Hourly",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    _spawn_open_child(store, rid, status="running", created_at=recent_created)

    with store.pool.connection() as conn:
        blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
    assert blocked is True, "a recent in-flight child must still block"


def test_has_open_previous_tick_conn_failed_child_never_blocks(
    handler: TodoHandler, store: Store
) -> None:
    """Existing behavior preserved: a terminally-``child-failed`` child
    never blocks, regardless of age or the new ``stuck_hours`` bound."""
    resp = handler.put(
        text="Hourly",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    child_id = _spawn_open_child(store, rid, status="open", created_at=recent_created)
    store.add_tag(child_id, Tag.open("child-failed:999"), set_by="system")

    with store.pool.connection() as conn:
        blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
    assert blocked is False, "a failed tick must not block regardless of age"


def _mint_job_under_child(store: Store, child_id: int, *, status: str) -> int:
    """Hand-mint a ``kind='job'`` under a spawned tick child with a given
    STATUS, for :func:`_has_open_previous_tick_conn` unit tests."""
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="tick job",
        meta={"job_type": "release_poll"},
        parent_id=child_id,
    )
    store.add_tag(job.id, Tag.closed("STATUS", status), set_by="system")
    return int(job.id)


def test_has_open_previous_tick_conn_terminal_job_never_blocks(
    handler: TodoHandler, store: Store
) -> None:
    """A still-open tick child whose dispatched job already went
    terminal must not block — the auto_check flip to ``STATUS:done``
    lags the job's own terminal write (gr592215). ``cancelled`` is
    deliberately absent: it does not settle the tick (next test)."""
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    for job_status in ("succeeded", "failed"):
        resp = handler.put(
            text="Hourly",
            meta={"schedule": {"cron": "0 * * * *"}},
        )
        rid = _id_of(resp.body)
        child_id = _spawn_open_child(
            store, rid, status="open", created_at=recent_created
        )
        _mint_job_under_child(store, child_id, status=job_status)

        with store.pool.connection() as conn:
            blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
        assert blocked is False, (
            f"a tick child whose job is {job_status} is settled, not in flight"
        )


def test_has_open_previous_tick_conn_cancelled_job_still_blocks(
    handler: TodoHandler, store: Store
) -> None:
    """A ``cancelled`` job does NOT settle its tick: the run never did
    its work and dispatch re-mints a fresh job under the same still-open
    tick todo ("cancelled → re-mint immediately"). Exempting it would
    mint the next tick alongside the old tick's retry — overlapping
    runs of one recurring."""
    resp = handler.put(
        text="Hourly",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    child_id = _spawn_open_child(
        store, rid, status="open", created_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    _mint_job_under_child(store, child_id, status="cancelled")

    with store.pool.connection() as conn:
        blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
    assert blocked is True, (
        "a cancelled job is owed work (dispatch re-mints), not settled — "
        "the tick must keep blocking"
    )


def _mint_todo_under_child(store: Store, child_id: int, *, status: str) -> int:
    """Hand-mint a ``kind='todo'`` under a spawned tick child with a
    given STATUS — the shape a tick's job leaves when its agentic work
    spawned follow-up todos."""
    todo = store.insert_ref(
        kind="todo",
        slug=None,
        title="tick sub-todo",
        meta={},
        parent_id=child_id,
    )
    store.add_tag(todo.id, Tag.closed("STATUS", status), set_by="system")
    return int(todo.id)


def test_has_open_previous_tick_conn_live_child_todo_still_blocks(
    handler: TodoHandler, store: Store
) -> None:
    """A settled job does not settle the tick while the tick's own child
    todos are still live — mirrors ``child_job_succeeded``'s completion
    guard (a succeeded job is not "done" while sibling work remains).
    Only ``done`` / ``won't-do`` child todos count as finished."""
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    for todo_status in ("open", "doing"):
        resp = handler.put(
            text="Hourly",
            meta={"schedule": {"cron": "0 * * * *"}},
        )
        rid = _id_of(resp.body)
        child_id = _spawn_open_child(
            store, rid, status="open", created_at=recent_created
        )
        _mint_job_under_child(store, child_id, status="succeeded")
        _mint_todo_under_child(store, child_id, status=todo_status)

        with store.pool.connection() as conn:
            blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
        assert blocked is True, (
            f"a live ({todo_status}) child todo keeps the tick in flight "
            "even with a settled job"
        )

    for todo_status in ("done", "won't-do"):
        resp = handler.put(
            text="Hourly",
            meta={"schedule": {"cron": "0 * * * *"}},
        )
        rid = _id_of(resp.body)
        child_id = _spawn_open_child(
            store, rid, status="open", created_at=recent_created
        )
        _mint_job_under_child(store, child_id, status="succeeded")
        _mint_todo_under_child(store, child_id, status=todo_status)

        with store.pool.connection() as conn:
            blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
        assert blocked is False, (
            f"a finished ({todo_status}) child todo does not keep the tick in flight"
        )


def test_has_open_previous_tick_conn_live_job_still_blocks(
    handler: TodoHandler, store: Store
) -> None:
    """A genuinely in-flight tick — a queued or running job under the
    still-open child — still blocks: that's real pile-up, not lag."""
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    for job_status in ("queued", "running"):
        resp = handler.put(
            text="Hourly",
            meta={"schedule": {"cron": "0 * * * *"}},
        )
        rid = _id_of(resp.body)
        child_id = _spawn_open_child(
            store, rid, status="open", created_at=recent_created
        )
        _mint_job_under_child(store, child_id, status=job_status)

        with store.pool.connection() as conn:
            blocked = _has_open_previous_tick_conn(conn, rid, stuck_hours=1.0)
        assert blocked is True, (
            f"a tick child whose job is {job_status} is still in flight"
        )


def test_schedule_pass_succeeded_job_on_open_tick_does_not_wedge(
    handler: TodoHandler, store: Store
) -> None:
    """Regression for gr592215: a spawned tick whose job succeeded but
    whose todo still reads ``STATUS:open`` (the auto_check flip hasn't
    landed) must not skip the next tick — the collision guard keys on
    the job's terminal state, not the lagging todo tag."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)

    # The prior tick: spawned recently, still STATUS:open, and its
    # dispatched job already succeeded — the exact wedge shape.
    old_created = datetime.now(UTC) - timedelta(minutes=15)
    old_stamp = old_created.isoformat(timespec="minutes")
    child_id = _spawn_open_child(store, rid, status="open", created_at=old_created)
    _mint_job_under_child(store, child_id, status="succeeded")

    _set_last_tick(store, rid, old_created)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1, "a settled prior tick must not block the next one"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL "
            "AND meta->>'spawned_for_tick' <> %s",
            (rid, old_stamp),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1, "a fresh tick should have been minted"


def test_schedule_pass_cancelled_job_on_open_tick_still_skips(
    handler: TodoHandler, store: Store
) -> None:
    """Regression: a spawned tick whose only job was ``cancelled`` is
    still in flight — dispatch re-mints the owed work under the same
    still-open todo — so the pass must skip rather than run a fresh tick
    alongside the old tick's retry."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)

    old_created = datetime.now(UTC) - timedelta(minutes=15)
    old_stamp = old_created.isoformat(timespec="minutes")
    child_id = _spawn_open_child(store, rid, status="open", created_at=old_created)
    _mint_job_under_child(store, child_id, status="cancelled")

    _set_last_tick(store, rid, old_created)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 0, "a cancelled-job tick is still in flight — no new tick"
    assert result.failed >= 1, "the due tick should have been skipped"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL "
            "AND meta->>'spawned_for_tick' <> %s",
            (rid, old_stamp),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 0, "no fresh tick may mint over the pending re-mint"


# ── settled-tick close (gr601246) ──────────────────────────────────


def _status_of(store: Store, ref_id: int) -> str:
    """The ref's current ``STATUS:<value>`` (default ``open``)."""
    return next(
        (str(t.value) for t in store.tags_for(ref_id) if t.prefix == "STATUS"),
        "open",
    )


def test_schedule_pass_closes_settled_tick_when_minting_successor(
    handler: TodoHandler, store: Store
) -> None:
    """gr601246: a spawned tick whose job settled flips ``STATUS:done``
    on the pass that mints its successor — the auto_check flip is a
    sampled sweep that can lag for hours, leaving every tick instance
    open (6/h on release_poll)."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    old_created = datetime.now(UTC) - timedelta(minutes=15)
    child_id = _spawn_open_child(store, rid, status="open", created_at=old_created)
    _mint_job_under_child(store, child_id, status="succeeded")

    _set_last_tick(store, rid, old_created)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1, "a settled prior tick must not block the next one"
    assert _status_of(store, child_id) == "done"
    assert result.extra["ticks_closed"] >= 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id = %s "
            "AND source = 'schedule' AND event = 'close'",
            (rid,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1, "the close must land a schedule:close audit event"


def test_schedule_pass_failed_job_tick_closes_wont_do(
    handler: TodoHandler, store: Store
) -> None:
    """gr601246: a tick whose job *failed* (``child-failed`` bubble and
    all) closes ``won't-do``, not ``done``."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    old_created = datetime.now(UTC) - timedelta(minutes=15)
    child_id = _spawn_open_child(store, rid, status="open", created_at=old_created)
    _mint_job_under_child(store, child_id, status="failed")
    store.add_tag(child_id, Tag.open("child-failed:999"), set_by="system")

    _set_last_tick(store, rid, old_created)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1, "a failed tick is terminal-for-scheduling — next tick mints"
    assert _status_of(store, child_id) == "won't-do"


def test_schedule_pass_settled_tick_closes_even_without_due_tick(
    handler: TodoHandler, store: Store
) -> None:
    """The per-recurring close runs on every claim, not only when a
    tick is due: a settled instance must not wait for the next
    cadence boundary to leave the open queue."""
    resp = handler.put(
        text="Daily",
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rid = _id_of(resp.body)
    old_created = datetime.now(UTC) - timedelta(hours=3)
    child_id = _spawn_open_child(store, rid, status="open", created_at=old_created)
    _mint_job_under_child(store, child_id, status="succeeded")

    # Last spawn was minutes ago — nothing is due this pass.
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(minutes=5))
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 0, "no tick is due yet"
    assert _status_of(store, child_id) == "done"


def test_schedule_pass_running_job_tick_stays_open(
    handler: TodoHandler, store: Store
) -> None:
    """A tick whose job is still running is genuinely in flight: it
    stays ``STATUS:open`` and still blocks the next tick — only a
    *terminal* job settles the instance."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    child_id = _spawn_open_child(store, rid, status="open", created_at=recent_created)
    _mint_job_under_child(store, child_id, status="running")

    _set_last_tick(store, rid, recent_created)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 0, "an in-flight tick still blocks the next tick"
    assert _status_of(store, child_id) == "open"


def test_schedule_pass_cancelled_job_tick_stays_open(
    handler: TodoHandler, store: Store
) -> None:
    """A ``cancelled`` job is owed work (dispatch re-mints under the
    same tick), not a settlement — the close must not retire the tick
    out from under the pending re-mint."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    recent_created = datetime.now(UTC) - timedelta(minutes=5)
    child_id = _spawn_open_child(store, rid, status="open", created_at=recent_created)
    _mint_job_under_child(store, child_id, status="cancelled")

    _set_last_tick(store, rid, recent_created)
    run_schedule_pass(store, limit=50)
    assert _status_of(store, child_id) == "open"


def test_schedule_pass_jobless_tick_stays_open(
    handler: TodoHandler, store: Store
) -> None:
    """A spawned tick with no job under it has work that was never
    dispatched (plain agentic tick, or dispatch pending) — nothing is
    terminal, so hygiene must not close it."""
    resp = handler.put(
        text="Daily",
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rid = _id_of(resp.body)
    child_id = _spawn_open_child(
        store, rid, status="open", created_at=datetime.now(UTC) - timedelta(hours=3)
    )

    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(minutes=5))
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 0
    assert _status_of(store, child_id) == "open"
    assert result.extra["ticks_closed"] == 0


def test_sweep_closes_settled_instance_under_paused_recurring(
    handler: TodoHandler, store: Store
) -> None:
    """Hygiene backstop: the per-recurring close only runs for claimed
    recurrings, so a paused recurring's settled instance — older than
    one cadence — is closed by the global sweep; a settled instance
    *younger* than one cadence is left for a later pass."""
    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    stale_id = _spawn_open_child(
        store, rid, status="open", created_at=datetime.now(UTC) - timedelta(hours=1)
    )
    fresh_id = _spawn_open_child(
        store, rid, status="open", created_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    _mint_job_under_child(store, stale_id, status="succeeded")
    _mint_job_under_child(store, fresh_id, status="succeeded")
    store.add_tag(
        rid,
        Tag.closed("STATUS", "paused"),
        set_by="agent",
        replace_prefix=True,
    )

    result = run_schedule_pass(store, limit=50)
    assert _status_of(store, stale_id) == "done", (
        "settled instance older than one cadence closes under a paused recurring"
    )
    assert _status_of(store, fresh_id) == "open", (
        "a settled instance younger than one cadence is left for a later pass"
    )
    assert result.extra["ticks_closed"] == 1


def test_sweep_pages_past_unclosable_instances(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression td602154: the sweep applied ``LIMIT`` *before* the
    cadence-eligibility check, so a full page of unclosable rows — here
    a yearly cadence ``tick_gap_before`` can't resolve inside its bound
    — re-headed every pass and starved the eligible instance behind
    them. The keyset scan must advance past skipped rows."""
    monkeypatch.setattr("precis.workers.schedule.worker._SWEEP_LIMIT", 3)
    resp = handler.put(
        text="Yearly",
        meta={"schedule": {"cron": "0 0 1 1 *"}},
    )
    yearly_id = _id_of(resp.body)
    stuck_ids = [
        _spawn_open_child(
            store,
            yearly_id,
            status="open",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
        for _ in range(3)
    ]
    for cid in stuck_ids:
        _mint_job_under_child(store, cid, status="succeeded")

    resp = handler.put(
        text="Every-10-min poll",
        meta={"schedule": {"cron": "*/10 * * * *"}},
    )
    rid = _id_of(resp.body)
    eligible_id = _spawn_open_child(
        store,
        rid,
        status="open",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    _mint_job_under_child(store, eligible_id, status="succeeded")

    closed = sweep_settled_tick_instances(store, now=datetime.now(UTC))
    assert closed == 1, (
        "the eligible instance behind a full page of unclosable rows must still close"
    )
    assert _status_of(store, eligible_id) == "done"
    for cid in stuck_ids:
        assert _status_of(store, cid) == "open", (
            "an unresolvable cadence stays open for the stalled-recurring alert"
        )


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
                "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL",
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
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL",
            (rid,),
        ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 0
    _ = result


def test_candidate_cron_with_stale_done_still_eligible(
    handler: TodoHandler, store: Store
) -> None:
    """Regression: a **cron**-shaped recurring that picked up a stray
    ``STATUS:done`` (e.g. hand-tagged, or a legacy artifact) must stay a
    dispatch candidate — only ``paused`` may exclude a cron watch. Before
    the fix, the one-shot's done-class exclusion set
    (``done``/``won't-do``/``auto-timeout``) was applied to *all*
    recurrings, permanently dropping a done-tagged cron (news_poll and
    several cast watches) out of ``_candidate_recurring_ids`` for good —
    the 2-day prod outage this test guards against."""
    from precis.workers.schedule.worker import _candidate_recurring_ids

    resp = handler.put(
        text="Hourly cron, stale done",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    store.add_tag(
        rid,
        Tag.closed("STATUS", "done"),
        set_by="agent",
        replace_prefix=True,
    )
    assert rid in _candidate_recurring_ids(store, limit=50)


def test_candidate_cron_paused_still_excluded(
    handler: TodoHandler, store: Store
) -> None:
    """A cron watch's one live exclusion — ``paused`` — must still work
    after scoping the extended (one-shot) exclusion set to one-shots."""
    from precis.workers.schedule.worker import _candidate_recurring_ids

    resp = handler.put(
        text="Hourly cron, paused",
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    store.add_tag(
        rid,
        Tag.closed("STATUS", "paused"),
        set_by="agent",
        replace_prefix=True,
    )
    assert rid not in _candidate_recurring_ids(store, limit=50)


def test_candidate_one_shot_with_done_excluded(
    handler: TodoHandler, store: Store
) -> None:
    """A **one-shot** (``schedule.at``) recurring with ``STATUS:done`` —
    the shape :func:`_process_one_shot` self-tags on resolve — is
    correctly excluded (unchanged behaviour, still the extended set)."""
    from precis.workers.schedule.worker import _candidate_recurring_ids

    resp = handler.put(
        text="One-shot, resolved",
        meta={"schedule": {"at": "2026-06-12T09:00:00Z"}},
    )
    rid = _id_of(resp.body)
    store.add_tag(
        rid,
        Tag.closed("STATUS", "done"),
        set_by="agent",
        replace_prefix=True,
    )
    assert rid not in _candidate_recurring_ids(store, limit=50)


# ── push delivery (cron-folded-into-recurring — folded from kind='cron') ──────────────


def test_schedule_pass_delivers_instead_of_spawning(
    handler: TodoHandler, store: Store
) -> None:
    """A recurring tick with ``meta.deliver`` set fires the push notify and
    stamps a ``schedule:deliver`` event — no subtask is minted into the
    queue (unlike the plain queue-mode path)."""
    resp = handler.put(
        text="check the api monitor",
        meta={
            "schedule": {"cron": "0 0 * * *"},
            "deliver": {"target": "conv:discord/g/c/t"},
        },
    )
    rid = _id_of(resp.body)
    yesterday_tick = datetime.now(UTC) - timedelta(hours=25)
    _set_last_tick(store, rid, yesterday_tick)

    result = run_schedule_pass(store, limit=50)
    assert result.ok >= 1

    with store.pool.connection() as conn:
        n_children_row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL",
            (rid,),
        ).fetchone()
        n_deliver_events_row = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id = %s "
            "AND source = 'schedule' AND event = 'deliver'",
            (rid,),
        ).fetchone()
    assert n_children_row is not None
    assert n_deliver_events_row is not None
    assert int(n_children_row[0]) == 0, "delivery-mode ticks must not mint a subtask"
    assert int(n_deliver_events_row[0]) == 1


def test_schedule_pass_deliver_tick_is_idempotent_same_minute(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="check the api monitor",
        meta={
            "schedule": {"cron": "0 0 * * *"},
            "deliver": {"target": "conv:discord/g/c/t"},
        },
    )
    rid = _id_of(resp.body)
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=25))
    run_schedule_pass(store, limit=50)
    run_schedule_pass(store, limit=50)
    with store.pool.connection() as conn:
        n_row = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id = %s "
            "AND source = 'schedule' AND event = 'deliver'",
            (rid,),
        ).fetchone()
    assert n_row is not None
    assert int(n_row[0]) == 1


# ── one-shot `at` resolve-and-retire ──────────────────────


def test_schedule_pass_fires_due_one_shot_and_retires(
    handler: TodoHandler, store: Store
) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    resp = handler.put(
        text="ask about the PR status",
        meta={
            "schedule": {"at": past, "catch_up": True},
            "deliver": {"target": "conv:discord/g/c/t"},
        },
    )
    rid = _id_of(resp.body)

    result = run_schedule_pass(store, limit=50)
    assert result.ok >= 1

    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:done" in tags
    with store.pool.connection() as conn:
        n_children_row = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND retired_at IS NULL",
            (rid,),
        ).fetchone()
    assert n_children_row is not None
    assert int(n_children_row[0]) == 0

    # A second pass is a no-op — the one-shot already retired (STATUS:done
    # excludes it from the next candidate scan).
    result2 = run_schedule_pass(store, limit=50)
    assert result2.claimed == 0


def test_schedule_pass_one_shot_not_yet_due_is_untouched(
    handler: TodoHandler, store: Store
) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = handler.put(
        text="future reminder",
        meta={"schedule": {"at": future}},
    )
    rid = _id_of(resp.body)
    run_schedule_pass(store, limit=50)
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:done" not in tags


def test_schedule_pass_expires_overdue_one_shot_without_catch_up(
    handler: TodoHandler, store: Store
) -> None:
    long_overdue = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    resp = handler.put(
        text="stale reminder",
        meta={"schedule": {"at": long_overdue, "catch_up": False}},
    )
    rid = _id_of(resp.body)
    run_schedule_pass(store, limit=50)
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:done" in tags
    with store.pool.connection() as conn:
        action_row = conn.execute(
            "SELECT payload->>'action' FROM ref_events WHERE ref_id = %s "
            "AND source = 'schedule' AND event = 'deliver'",
            (rid,),
        ).fetchone()
    assert action_row is not None
    assert action_row[0] == "expire"
