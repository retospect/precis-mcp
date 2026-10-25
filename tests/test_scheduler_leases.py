"""Slice 10 / §15i: the decentralized recurring-work lease claim.

``claim_scheduler_lease`` is the reserve-at-claim pattern (§5.2) applied to
*time* — an atomic conditional advance where the ``UPDATE`` matching the due row
IS the lock. This proves exactly-once + re-arm semantics against real PG (so the
seed + conditional-advance SQL runs for real). Cadence names are unique per test
because ``scheduler_leases`` is a global table on the shared test DB.

The exactly-once guarantee rests on Postgres: ``now()`` is fixed for a
transaction, so seed-then-advance in one tx is deterministic (the freshly-seeded
``next_fire_at = now()`` always satisfies ``next_fire_at <= now()`` on the first
claim), and a second claim inside the interval always loses.
"""

from __future__ import annotations

from uuid import uuid4


def _name() -> str:
    return f"cad-{uuid4().hex}"


def test_first_claim_seeds_and_fires(store) -> None:
    name = _name()
    assert store.claim_scheduler_lease(name, 60, "h1") is True
    lease = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert lease.interval_s == 60
    assert lease.last_host == "h1"
    assert lease.last_fired_at is not None


def test_not_due_again_within_interval(store) -> None:
    name = _name()
    assert store.claim_scheduler_lease(name, 3600, "h1") is True
    # advanced to now()+1h → every further claim this interval loses (the advance
    # is the lock: exactly-once across however many workers race it).
    assert store.claim_scheduler_lease(name, 3600, "h2") is False
    assert store.claim_scheduler_lease(name, 3600, "h3") is False


def test_rearms_and_any_host_can_win(store) -> None:
    name = _name()
    assert store.claim_scheduler_lease(name, 60, "h1") is True
    assert store.claim_scheduler_lease(name, 60, "h1") is False
    # simulate the interval elapsing (a real cadence would just wait it out).
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE scheduler_leases SET next_fire_at = now() - interval '5 seconds' "
            "WHERE name = %s",
            (name,),
        )
    # due again → a *different* host mints it (decentralized: whichever live
    # worker gets there first, not a designated node).
    assert store.claim_scheduler_lease(name, 60, "h2") is True
    lease = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert lease.last_host == "h2"


def test_claim_resyncs_a_shrunk_interval(store) -> None:
    """gr518038: an interval SHRINK (gripe_triage's ~24h→15m) must not
    leave the new cadence waiting out the old far-future ``next_fire_at``
    — the claim's ``_RESYNC_LEASE`` clamps the clock to ``now() + new
    interval``, so it fires within the new interval."""
    name = _name()
    # What the old large-interval cadence left behind: re-armed a day out.
    assert store.claim_scheduler_lease(name, 24 * 3600, "h1") is True
    # The next claim with the new interval resyncs the row before the
    # advance — the (now-sooner) fire is not due yet, so the claim loses.
    assert store.claim_scheduler_lease(name, 900, "h1") is False
    lease = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert lease.interval_s == 900
    with store.pool.connection() as conn:
        clamped = conn.execute(
            "SELECT next_fire_at <= now() + make_interval(secs => %s) "
            "FROM scheduler_leases WHERE name = %s",
            (900, name),
        ).fetchone()[0]
    assert clamped
    # Once the new interval passes the claim wins — a 15-minute wait,
    # not a day.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE scheduler_leases SET next_fire_at = now() - interval '1 second' "
            "WHERE name = %s",
            (name,),
        )
    assert store.claim_scheduler_lease(name, 900, "h2") is True


def test_claim_keeps_the_sooner_clock_on_a_grow(store) -> None:
    """The ``LEAST`` half of gr518038: an interval GROW resyncs
    ``interval_s`` but keeps an already-sooner ``next_fire_at`` — a
    longer cadence must not push a pending fire further out."""
    name = _name()
    assert store.claim_scheduler_lease(name, 60, "h1") is True
    before = {ln.name: ln for ln in store.scheduler_leases()}[name]
    # Resynced to 3600 but the +60s clock stands; the claim loses because
    # the (sooner) fire is still pending.
    assert store.claim_scheduler_lease(name, 3600, "h2") is False
    after = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert after.interval_s == 3600
    assert after.next_fire_at == before.next_fire_at
    assert after.last_fired_at == before.last_fired_at


def test_seed_and_claim_leave_a_steady_interval_untouched(store) -> None:
    """Steady state writes nothing (gr518038's ``interval_s <>`` guard +
    insert-only seed): re-seeding an existing row is a no-op, and a
    re-claim inside the same interval leaves the row byte-identical."""
    name = _name()
    store.seed_scheduler_lease(name, 60)
    before = {ln.name: ln for ln in store.scheduler_leases()}[name]
    store.seed_scheduler_lease(name, 60)
    after = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert after == before

    assert store.claim_scheduler_lease(name, 60, "h1") is True
    fired = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert store.claim_scheduler_lease(name, 60, "h2") is False
    again = {ln.name: ln for ln in store.scheduler_leases()}[name]
    assert again == fired


def test_interval_is_a_code_fact(store) -> None:
    """The interval passed by the caller (the code-side cadence registry) drives
    the advance and is stored for the console — a cadence change takes effect on
    the next claim, no migration."""
    name = _name()
    assert store.claim_scheduler_lease(name, 60, "h1") is True
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE scheduler_leases SET next_fire_at = now() WHERE name = %s",
            (name,),
        )
    assert store.claim_scheduler_lease(name, 900, "h1") is True
    assert {ln.name: ln for ln in store.scheduler_leases()}[name].interval_s == 900
