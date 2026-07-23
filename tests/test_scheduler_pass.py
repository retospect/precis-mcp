"""Slice 10 / §15i: the decentralized ``scheduler`` worker pass.

``run_scheduler_pass`` claims each due cadence's lease and fires its work
in-process; an undue cadence (or a lost lease) is a dark no-op. Tests inject
unique-named cadences (the lease table is a global on the shared DB) and also
exercise the *real* ``cron_tick`` cadence end to end — which, post-ADR-0061,
drives ``run_schedule_pass`` (the retired ``kind='cron'`` engine's replacement,
shared with the launchd ``precis cron tick`` timer and the default worker
rotation).
"""

from __future__ import annotations

from uuid import uuid4

from precis.store.types import Tag
from precis.workers.scheduler import Cadence, _run_cron_tick, run_scheduler_pass


def _cad(run, interval: int = 60) -> Cadence:
    return Cadence(name=f"c-{uuid4().hex}", interval_s=interval, run=run)


def test_fires_due_cadence_and_reports(store) -> None:
    ran: list[int] = []
    cad = _cad(lambda s, b: ran.append(b))
    r = run_scheduler_pass(store, host="h", batch_size=7, cadences=(cad,))
    assert (r.handler, r.claimed, r.ok, r.failed) == ("scheduler", 1, 1, 0)
    assert ran == [7]  # batch_size threaded through to the cadence work


def test_undue_cadence_is_dark(store) -> None:
    cad = _cad(lambda s, b: None, interval=3600)
    assert run_scheduler_pass(store, host="h", cadences=(cad,)).claimed == 1
    # second cycle: lease not due for an hour → claimed=0 so the loop idle-sleeps
    r2 = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (r2.claimed, r2.ok, r2.failed) == (0, 0, 0)


def test_raising_cadence_is_failed_and_does_not_refire(store) -> None:
    calls: list[int] = []

    def boom(s, b) -> None:
        calls.append(1)
        raise RuntimeError("cadence work blew up")

    cad = _cad(boom)
    r = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (r.claimed, r.ok, r.failed) == (1, 0, 1)
    # the lease already advanced (fire-and-forget, like the launchd timer) — a
    # raise does not re-fire until the next interval.
    r2 = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert r2.claimed == 0
    assert calls == [1]


def test_multiple_cadences_are_independent(store) -> None:
    hits: list[str] = []
    a = _cad(lambda s, b: hits.append("a"))
    b = _cad(lambda s, b: hits.append("b"), interval=3600)
    r = run_scheduler_pass(store, host="h", cadences=(a, b))
    assert r.claimed == 2 and r.ok == 2
    assert sorted(hits) == ["a", "b"]


def test_cron_tick_cadence_fires_a_due_one_shot(store) -> None:
    """The real ``cron_tick`` cadence resolves a due one-shot recurring —
    end-to-end cover for ``run_schedule_pass`` (ADR 0061's replacement for the
    retired ``kind='cron'`` engine, shared here with the §15i decentralized
    scheduler pass)."""
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="a long-overdue one-shot",
        meta={
            "schedule": {"at": "2020-01-01T00:00:00+00:00", "catch_up": True},
            "deliver": {"target": "conv:discord/g/c/t"},
        },
    )
    store.add_tag(ref.id, Tag.open("level:recurring"), set_by="agent")

    _run_cron_tick(store, 32)

    tags = {str(t) for t in store.tags_for(ref.id)}
    assert "STATUS:done" in tags  # one-shot resolved, self-retired
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id = %s "
            "AND source = 'schedule' AND event = 'deliver'",
            (ref.id,),
        ).fetchone()
    assert row is not None and int(row[0]) == 1


def test_scheduler_service_is_dark_by_default() -> None:
    """The service registers off — no default profile, gated only by the env
    flag / a service_config prio row — so the launchd timers still own the ticks
    until the Phase-2 cutover flips it on."""
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["scheduler"]
    assert spec.default_profiles == frozenset()
    assert spec.enable_env == "PRECIS_SCHEDULER_ENABLED"
    assert spec.ref_pass is True
