"""Slice 10 / §15i: the decentralized ``scheduler`` worker pass.

``run_scheduler_pass`` claims each due cadence's lease and fires its work
in-process; an undue cadence (or a lost lease) is a dark no-op. Tests inject
unique-named cadences (the lease table is a global on the shared DB) and also
exercise the *real* ``cron_tick`` cadence end to end — which drives the
``fire_due_cron`` core extracted from ``precis cron tick`` (previously untested).
"""

from __future__ import annotations

import re
from uuid import uuid4

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


def test_cron_tick_cadence_fires_a_due_entry(store, hub) -> None:
    """The real cron_tick cadence advances a due cron entry — end-to-end cover
    for the extracted ``fire_due_cron`` (the shared cron engine, §15i)."""
    from precis.handlers.cron import CronHandler

    cron = CronHandler(hub=hub)
    ack = cron.put(
        text="a long-overdue one-shot",
        when="2020-01-01T00:00:00Z",
        target="conv:discord/g/c/t",
        catch_up=True,  # missed one-shot fires rather than expiring
    ).body
    ref_id = int(re.search(r"id=(\d+)", ack).group(1))

    _run_cron_tick(store, 32)

    ref = store.get_ref(kind="cron", id=ref_id)
    assert ref.meta["status"] == "fired"
    assert int(ref.meta.get("fire_count", 0)) >= 1


def test_scheduler_service_is_dark_by_default() -> None:
    """The service registers off — no default profile, gated only by the env
    flag / a service_config prio row — so the launchd timers still own the ticks
    until the Phase-2 cutover flips it on."""
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["scheduler"]
    assert spec.default_profiles == frozenset()
    assert spec.enable_env == "PRECIS_SCHEDULER_ENABLED"
    assert spec.ref_pass is True
