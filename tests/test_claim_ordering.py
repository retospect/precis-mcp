"""Claim ordering — prio ASC (lower = more urgent), then age (slice 6a).

``claim_executor_jobs`` was pure FIFO (``ORDER BY ref_id``). 6a makes it
``ORDER BY COALESCE(prio, 5) ASC, ref_id ASC`` — LOWER ``refs.prio`` claims
first. This direction is not a free choice: it's the ``0014_refs_prio.sql``
convention every prio *writer* in the codebase already follows (prio=1 ==
chat/preempt, prio=2 == cron, prio=5 == default/NULL; lower number == more
urgent), and the dispatcher propagates a parent todo's prio verbatim onto
the jobs it mints (``dispatch.py``, ``claude_inproc.py``'s auto-decompose
mint) — so an urgent (low-number) todo must produce a job that claims
*ahead* of commodity work, not behind it. A prior version of this claim
sorted ``DESC`` (highest-number first), which silently inverted that
convention: a ``PRIO:urgent`` (prio=1) todo minted a job that claimed DEAD
LAST. These tests PIN the ``0014`` (ASC) direction so a future
"optimization" that flips the claim back to ``DESC`` fails loudly here
instead of quietly re-introducing that inversion.

Oldest-first (``ref_id`` ASC) breaks ties within a prio band (anti-
starvation), and an all-unset queue stays FIFO. A synthetic executor name
isolates each test's queue from any other jobs sharing the test DB.

The release band (gr584136): the four release-pass legs
(``ci_verdict_poll``, ``release_cut``, ``release_deploy``,
``release_confirm``) plus the dark-factory / release heartbeat ticks
(``release_poll``, ``factory_rescue_tick``, ``ci_watch``,
``plan_dispatch`` — td593845) carry class rank 2.5 — derived from
``job_type``, strictly between recurring (2) and background (3) — so a
release's 10-minute poll cadence and the factory's ticks can't be
stretched by an unrelated ``derived_drain`` backlog, while still
ranking behind recurring work.
While a band row is claimable, class-ageing promotion is frozen
lane-wide, so no aged background row can reach or cross the band.
"""

from __future__ import annotations

from typing import Any

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import claim_executor_jobs


def _queue_job(
    store: Store,
    *,
    executor: str,
    prio: int | None,
    job_type: str = "demo",
    origin: str | None = None,
    queued_s_ago: float = 0,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Insert a ``STATUS:queued`` job for ``executor`` with the given prio."""
    meta: dict[str, Any] = {
        "job_type": job_type,
        "executor": executor,
        "params": {},
        **(extra_meta or {}),
    }
    if origin is not None:
        meta["origin"] = origin
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"job prio={prio}",
        meta=meta,
        prio=prio,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    if queued_s_ago:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags rt SET created_at = now() - "
                "make_interval(secs => %s) "
                "FROM tags t WHERE t.tag_id = rt.tag_id AND rt.ref_id = %s "
                "AND t.namespace = 'STATUS' AND t.value = 'queued'",
                (queued_s_ago, ref.id),
            )
            conn.commit()
    return ref.id


def _claim_order(
    store: Store, executor: str, *, reserve_host_id: str | None = None
) -> list[int]:
    """The ref_ids the claim would lock, in order (rolled back — no side effects)."""
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn, executor=executor, limit=50, reserve_host_id=reserve_host_id
        )
        conn.rollback()
    return [r[0] for r in rows]


def _advertise(store: Store, host: str, slots: dict[str, int | None]) -> None:
    """Seed ``resource_slots`` AND a fresh ``host_heartbeat`` row — the
    placement gate (``_placement_ok``) reads both, mirroring
    ``tests/test_reserve_at_claim.py``'s ``_advertise``."""
    store.sync_host_resource_slots(host, slots)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host,),
        )
        conn.commit()


def test_lower_prio_claimed_first(store: Store) -> None:
    ex = "test_order_prio_first"
    low_number_urgent = _queue_job(store, executor=ex, prio=1)
    mid = _queue_job(store, executor=ex, prio=5)
    high_number_commodity = _queue_job(store, executor=ex, prio=9)
    # Insertion order urgent→mid→commodity; claim order is prio ASC (0014:
    # lower number == more urgent), so the urgent (prio=1) job claims first
    # and the commodity (prio=9) job claims last.
    assert _claim_order(store, ex) == [
        low_number_urgent,
        mid,
        high_number_commodity,
    ]


def test_same_prio_orders_by_age(store: Store) -> None:
    ex = "test_order_same_prio"
    first = _queue_job(store, executor=ex, prio=5)
    second = _queue_job(store, executor=ex, prio=5)
    third = _queue_job(store, executor=ex, prio=5)
    # Equal prio → oldest (smallest ref_id) first: the pre-6a FIFO tiebreak,
    # unaffected by the ASC/DESC direction (anti-starvation).
    assert _claim_order(store, ex) == [first, second, third]


def test_unset_prio_is_fifo(store: Store) -> None:
    ex = "test_order_unset"
    a = _queue_job(store, executor=ex, prio=None)
    b = _queue_job(store, executor=ex, prio=None)
    # All NULL → COALESCE default (5) for everyone → age order (FIFO).
    assert _claim_order(store, ex) == [a, b]


def test_null_prio_ranks_as_default_midpoint(store: Store) -> None:
    ex = "test_order_null_midpoint"
    urgent = _queue_job(store, executor=ex, prio=3)
    unset = _queue_job(store, executor=ex, prio=None)  # ranks as 5 (0014 default)
    commodity = _queue_job(store, executor=ex, prio=8)
    # 3 (more urgent) < 5 (NULL default) < 8 (least urgent) — the unset job
    # slots into the middle band, on both sides: prio=3 beats NULL, and NULL
    # beats prio=8.
    assert _claim_order(store, ex) == [urgent, unset, commodity]


# ── Release band (gr584136, S3 of td584381) ────────────────────────────

#: The release pass's legs — the rank derives from ``job_type`` alone
#: (their ``meta.origin`` stays ``background`` today).
_RELEASE_LEG_TYPES = (
    "ci_verdict_poll",
    "release_cut",
    "release_deploy",
    "release_confirm",
)


def test_release_leg_claims_ahead_of_background_backlog(store: Store) -> None:
    """gr584136 repro: a re-queued ``ci_verdict_poll`` (the wake_runner's
    re-queue is a fresh ``STATUS:queued`` stamp — age 0) behind a 50-deep
    ``derived_drain`` backlog claims FIRST — within one worker-slot
    turnover — instead of at position 51."""
    ex = "test_release_leg_first"
    drains = [
        _queue_job(
            store,
            executor=ex,
            prio=5,
            job_type="derived_drain",
            origin="background",
        )
        for _ in range(50)
    ]
    poll = _queue_job(
        store,
        executor=ex,
        prio=5,
        job_type="ci_verdict_poll",
        origin="background",
    )
    order = _claim_order(store, ex, reserve_host_id="h1")
    assert order[0] == poll
    # ``derived_drain``'s code-shipped running cap (default 2) lets only
    # two drains claim per call — the backlog's internal order is
    # unchanged (queued_since FIFO).
    assert order[1:] == drains[:2]


def test_release_leg_never_outranks_recurring(store: Store) -> None:
    """The band is pinned at 2.5 — a release row never class-promotes, so
    it stays behind a fresh recurring row even after waiting an hour."""
    ex = "test_release_below_recurring"
    poll = _queue_job(
        store,
        executor=ex,
        prio=5,
        job_type="ci_verdict_poll",
        origin="background",
        queued_s_ago=3600,
    )
    rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
    assert _claim_order(store, ex) == [rec, poll]


def test_background_order_unchanged_without_release_leg(store: Store) -> None:
    """With no release leg queued the band is inert: background rows keep
    their existing queued_since order."""
    ex = "test_no_release_bg_order"
    a = _queue_job(store, executor=ex, prio=5, origin="background")
    b = _queue_job(store, executor=ex, prio=5, origin="background")
    assert _claim_order(store, ex) == [a, b]


def test_all_release_legs_share_the_band(store: Store) -> None:
    """release_cut/release_deploy/release_confirm get the same rank as
    ci_verdict_poll: each loses to a fresh recurring row and beats an
    older background row — identical position in the class order."""
    # release_cut/deploy/confirm declare ``placement={repo_checkout, git}``
    # (registry.py) — the claim host must advertise both or the gate drops
    # the row; ci_verdict_poll (job_inproc, DB/HTTP-only) declares none.
    _advertise(store, "h1", {"repo_checkout": 1, "git": 1})
    for job_type in _RELEASE_LEG_TYPES:
        ex = f"test_release_leg_{job_type}"
        bg = _queue_job(
            store,
            executor=ex,
            prio=8,
            origin="background",
            queued_s_ago=300,
        )
        leg = _queue_job(
            store, executor=ex, prio=5, job_type=job_type, origin="background"
        )
        rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
        assert _claim_order(store, ex, reserve_host_id="h1") == [rec, leg, bg]


def test_aged_background_cannot_leapfrog_release_leg(store: Store) -> None:
    """The anti-leapfrog pin: without the freeze this 2h-old background
    row promotes to rank 2 and claims first; while the release leg is
    claimable no lower class promotes, so it stays last."""
    ex = "test_release_freeze"
    old_bg = _queue_job(
        store, executor=ex, prio=8, origin="background", queued_s_ago=7200
    )
    poll = _queue_job(
        store,
        executor=ex,
        prio=5,
        job_type="ci_verdict_poll",
        origin="background",
    )
    rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
    assert _claim_order(store, ex) == [rec, poll, old_bg]


# ── The waiver gripe's build in the band (Reto 2026-11-10) ─────────────
#
# A release-waiver gripe is a broken build: its chain's run_slice /
# land_slice jobs carry ``meta.claim_band = release`` and claim at the
# release legs' class (2.5) — behind recurring, ahead of every ordinary
# gripe build (origin ``background``, whatever its prio or age). The rank
# keys on the meta stamp alone, so these rows use the default job type
# (a real ``run_slice`` row would also face the placement gate).

_BAND = {"claim_band": "release"}


def test_waiver_build_claims_ahead_of_ordinary_gripe_builds(store: Store) -> None:
    ex = "test_waiver_band_first"
    builds = [
        _queue_job(
            store,
            executor=ex,
            prio=2,  # hotter than the waiver build's 5 (prio 1 is absolute)
            origin="background",
            queued_s_ago=3600,
        )
        for _ in range(3)
    ]
    waiver = _queue_job(
        store, executor=ex, prio=5, origin="background", extra_meta=_BAND
    )
    order = _claim_order(store, ex)
    assert order[0] == waiver and sorted(order[1:]) == sorted(builds)


def test_waiver_land_shares_the_band_with_the_release_legs(store: Store) -> None:
    ex = "test_waiver_band_land"
    bg = _queue_job(store, executor=ex, prio=8, origin="background")
    land = _queue_job(store, executor=ex, prio=5, origin="background", extra_meta=_BAND)
    rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
    assert _claim_order(store, ex) == [rec, land, bg]


def test_waiver_band_pins_like_a_release_leg(store: Store) -> None:
    """It never outranks recurring work, and an aged background row cannot
    leapfrog it (the release freeze applies to the band as a whole)."""
    ex = "test_waiver_band_pin"
    old_bg = _queue_job(
        store, executor=ex, prio=8, origin="background", queued_s_ago=7200
    )
    waiver = _queue_job(
        store,
        executor=ex,
        prio=5,
        origin="background",
        queued_s_ago=3600,
        extra_meta=_BAND,
    )
    rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
    assert _claim_order(store, ex) == [rec, waiver, old_bg]


def test_an_unknown_band_value_changes_nothing(store: Store) -> None:
    ex = "test_waiver_band_unknown"
    a = _queue_job(store, executor=ex, prio=5, origin="background")
    b = _queue_job(
        store,
        executor=ex,
        prio=5,
        origin="background",
        extra_meta={"claim_band": "other"},
    )
    assert _claim_order(store, ex) == [a, b]


# ── Heartbeat ticks in the band (gr592211, td593845 review) ───────────
#
# ``release_poll`` / ``factory_rescue_tick`` / ``ci_watch`` /
# ``plan_dispatch`` mint under recurring todos, so origin alone left
# them at class 2 — an aged background row promotes to tie them there
# and then wins ``queued_since`` (release_poll sat 15+ min behind a
# ``derived_drain`` backlog). In the band they self-pin at 2.5 and the
# freeze holds the backlog at class 3 while one is claimable.
_HEARTBEAT_JOB_TYPES = (
    "release_poll",
    "factory_rescue_tick",
    "ci_watch",
    "plan_dispatch",
)


def test_heartbeat_ticks_claim_ahead_of_aged_backlog(store: Store) -> None:
    """A heartbeat tick queued fresh behind a 2h-old background row
    claims first — without band membership the aged row promotes to
    class 2 and beats the tick on ``queued_since`` (gr592211)."""
    for job_type in _HEARTBEAT_JOB_TYPES:
        ex = f"test_heartbeat_{job_type}"
        old_bg = _queue_job(
            store,
            executor=ex,
            prio=8,
            origin="background",
            queued_s_ago=7200,
        )
        tick = _queue_job(
            store, executor=ex, prio=2, job_type=job_type, origin="recurring"
        )
        rec = _queue_job(store, executor=ex, prio=2, origin="recurring")
        # The tick never outranks recurring (2) but always beats the
        # frozen backlog (3), regardless of the backlog's age.
        assert _claim_order(store, ex) == [rec, tick, old_bg]
