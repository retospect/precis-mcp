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
