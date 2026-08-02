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


def test_scheduler_service_is_live_on_both_profiles() -> None:
    """§A: the service is default-on for BOTH profiles — the agent profile
    must run it too, or a host-pinned cadence (dream_agent, anki_sync) never
    has an eligible claimant."""
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["scheduler"]
    assert spec.default_profiles == frozenset({"system", "agent"})
    assert spec.enable_env is None
    assert spec.ref_pass is True


# ── §A: host affinity + local eligibility ───────────────────────────────


def _pinned_cad(
    run, *, host_affinity: str | None, eligible=None, interval: int = 60
) -> Cadence:
    return Cadence(
        name=f"c-{uuid4().hex}",
        interval_s=interval,
        run=run,
        host_affinity=host_affinity,
        eligible=eligible,
    )


def test_host_affinity_non_pinned_host_never_claims(store) -> None:
    """A pinned cadence is never even attempted on a non-affinity host — the
    lease stays unadvanced (still due) so the pinned host can claim it later,
    catch-up-late-not-lost rather than lost to a wrong-host steal."""
    ran: list[str] = []
    cad = _pinned_cad(lambda s, b: ran.append("fired"), host_affinity="melchior")

    r_wrong_host = run_scheduler_pass(store, host="caspar", cadences=(cad,))
    assert (r_wrong_host.claimed, r_wrong_host.ok, r_wrong_host.failed) == (0, 0, 0)
    assert ran == []
    # the lease was never even seeded — a fresh/absent row isn't in
    # scheduler_leases at all yet, proving the claim call was skipped, not
    # attempted-and-lost.
    names = {lease.name for lease in store.scheduler_leases()}
    assert cad.name not in names

    r_right_host = run_scheduler_pass(store, host="melchior", cadences=(cad,))
    assert (r_right_host.claimed, r_right_host.ok, r_right_host.failed) == (1, 1, 0)
    assert ran == ["fired"]


def test_ineligible_worker_skips_without_advancing_lease(store) -> None:
    """An ineligible worker never claims (the lease is untouched); a LATER
    eligible claimer still gets the fire immediately — drop-no-fire, not a
    delayed/stolen one."""
    is_eligible = False
    ran: list[str] = []
    cad = _pinned_cad(
        lambda s, b: ran.append("fired"),
        host_affinity=None,
        eligible=lambda: is_eligible,
    )

    r1 = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (r1.claimed, r1.ok, r1.failed) == (0, 0, 0)
    assert ran == []
    names = {lease.name for lease in store.scheduler_leases()}
    assert cad.name not in names  # eligibility gate is checked before any claim

    is_eligible = True
    r2 = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (r2.claimed, r2.ok, r2.failed) == (1, 1, 0)
    assert ran == ["fired"]


def test_exactly_once_under_two_concurrent_claimants(store) -> None:
    """Extends the single-threaded exactly-once cover in
    ``test_scheduler_leases.py`` with a REAL concurrent race at the pass
    level — two threads racing ``run_scheduler_pass`` for the same due
    cadence must sum to exactly one claim."""
    from concurrent.futures import ThreadPoolExecutor

    hits: list[str] = []
    cad = _cad(lambda s, b: hits.append("fired"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_scheduler_pass, store, host=h, cadences=(cad,))
            for h in ("h1", "h2")
        ]
        results = [f.result() for f in futures]

    assert sum(r.claimed for r in results) == 1
    assert hits == ["fired"]


def test_catch_up_long_overdue_lease_fires_once_no_backlog_burst(store) -> None:
    """A long-overdue lease (fleet-wide outage) collapses to exactly ONE
    catch-up fire, and re-arms to ``now() + interval`` — NOT
    ``old_next_fire_at + interval`` — so recovery never bursts a backlog."""
    from datetime import UTC, datetime

    hits: list[int] = []
    cad = _cad(lambda s, b: hits.append(1), interval=60)

    # Seed + immediately re-arm it once, then force it WAY overdue.
    assert run_scheduler_pass(store, host="h", cadences=(cad,)).claimed == 1
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE scheduler_leases SET next_fire_at = now() - interval '1 day' "
            "WHERE name = %s",
            (cad.name,),
        )
    before = datetime.now(UTC)

    r = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (r.claimed, r.ok, r.failed) == (1, 1, 0)
    assert hits == [1, 1]  # one catch-up fire (plus the seed fire above)

    lease = {ln.name: ln for ln in store.scheduler_leases()}[cad.name]
    # next_fire_at ~= now() + 60s, nowhere near old_next_fire_at (1 day ago) + 60s.
    delta_s = (lease.next_fire_at - before).total_seconds()
    assert 55 <= delta_s <= 90, f"expected ~60s from now, got {delta_s}s"

    # not due again immediately — no backlog burst.
    r2 = run_scheduler_pass(store, host="h", cadences=(cad,))
    assert r2.claimed == 0


def test_raising_resolve_interval_falls_back_and_spares_later_cadences(
    store,
) -> None:
    """A ``resolve_interval`` blip must not starve cadences ordered after it:
    the claim falls back to the static ``interval_s`` and the loop continues
    — same hardening contract as the ``eligible``/claim guards."""
    hits: list[str] = []

    def boom_resolver(s) -> int:
        raise RuntimeError("interval resolver blew up")

    a = Cadence(
        name=f"c-{uuid4().hex}",
        interval_s=60,
        run=lambda s, b: hits.append("a"),
        resolve_interval=boom_resolver,
    )
    b = _cad(lambda s, b: hits.append("b"))
    r = run_scheduler_pass(store, host="h", cadences=(a, b))
    # both fired: a via the static-interval fallback, b untouched by a's blip.
    assert (r.claimed, r.ok, r.failed) == (2, 2, 0)
    assert hits == ["a", "b"]


def test_dream_agent_cadence_interval_resolves_the_g_knob(store) -> None:
    """The ``dream_agent`` cadence's ``resolve_interval`` IS §G's
    ``resolve_min_interval_minutes`` (DB > env > compiled default), in
    seconds — not a separately-maintained constant."""
    from precis.workers import dream_throttle
    from precis.workers.scheduler import CADENCES

    dream_cad = next(c for c in CADENCES if c.name == "dream_agent")
    assert dream_cad.resolve_interval is not None
    assert dream_cad.host_affinity == "melchior"

    # compiled default (no DB row, no env).
    assert dream_cad.resolve_interval(store) == int(
        dream_throttle.DEFAULT_MIN_INTERVAL_MINUTES * 60
    )


def test_dream_agent_cadence_interval_env_override(store, monkeypatch) -> None:
    from precis.workers.scheduler import CADENCES

    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "5")
    dream_cad = next(c for c in CADENCES if c.name == "dream_agent")
    assert dream_cad.resolve_interval is not None
    assert dream_cad.resolve_interval(store) == 300


def test_dream_agent_cadence_interval_db_override_wins(store, monkeypatch) -> None:
    from precis.budget import settings as app_settings
    from precis.workers.dream_throttle import MIN_INTERVAL_KEY
    from precis.workers.scheduler import CADENCES

    monkeypatch.setenv("PRECIS_DREAM_MIN_INTERVAL_MINUTES", "5")
    app_settings.set_float(store, MIN_INTERVAL_KEY, 3.0)
    dream_cad = next(c for c in CADENCES if c.name == "dream_agent")
    assert dream_cad.resolve_interval is not None
    assert dream_cad.resolve_interval(store) == 180  # DB (3min) beats env (5min)


def test_anki_sync_cadence_ineligible_by_default(store, monkeypatch) -> None:
    """``PRECIS_ANKI_ENABLED`` unset (the test default) ⇒ the ``anki_sync``
    cadence's ``eligible`` gate is False, so it never claims."""
    monkeypatch.delenv("PRECIS_ANKI_ENABLED", raising=False)
    from precis.workers.scheduler import CADENCES

    anki_cad = next(c for c in CADENCES if c.name == "anki_sync")
    assert anki_cad.host_affinity == "melchior"
    assert anki_cad.eligible is not None
    assert anki_cad.eligible() is False

    r = run_scheduler_pass(store, host="melchior", cadences=(anki_cad,))
    assert (r.claimed, r.ok, r.failed) == (0, 0, 0)
    names = {lease.name for lease in store.scheduler_leases()}
    assert anki_cad.name not in names
