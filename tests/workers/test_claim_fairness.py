"""Slice 2 of job-scheduling-fairness: class rank before prio (D2),
class-preserving ageing (D3), prio-1 absolute across classes, and the
``app_state`` ``claim.max_running.<job_type>`` cap (D4 cap half).

Acceptance pins: a fresh prio-2 recurring row outranks a background row
still inside its promotion bound, but no background row waits more than
3 h on a live lane; a 31-min-old background row outranks a fresh
background prio-2 row but not a fresh recurring one; prio 1 beats
everything; ``max_running`` refuses the (cap+1)th claim and a capped
band cannot starve rows queued behind it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import (
    _MAX_RUNNING_DEFAULTS,
    CLAIM_CLASS_RANK_ENV,
    claim_executor_jobs,
)

_DRAIN_CAP = _MAX_RUNNING_DEFAULTS["derived_drain"]

_AGE_STEP_S = 600
_CLASS_AGE_S = 1800


def _job(
    store: Store,
    *,
    origin: str | None,
    prio: int | None,
    queued_s_ago: float = 0,
    job_type: str = "x",
    status: str = "queued",
    executor: str = "job_inproc",
    params: dict[str, Any] | None = None,
) -> int:
    meta: dict[str, Any] = {
        "job_type": job_type,
        "executor": executor,
        "params": dict(params or {}),
    }
    if origin is not None:
        meta["origin"] = origin
    if status == "running":
        meta["lease_host"] = "h1"
    ref = store.insert_ref(kind="job", slug=None, title="j", meta=meta, prio=prio)
    store.add_tag(int(ref.id), Tag.parse_strict(f"STATUS:{status}"))
    if queued_s_ago:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags SET created_at = now() - "
                "make_interval(secs => %s) WHERE ref_id = %s",
                (queued_s_ago, int(ref.id)),
            )
    return int(ref.id)


def _claim(store: Store, limit: int = 10, *, node: str | None = "h1") -> list[int]:
    """Claim on host ``h1``: ``node`` is the PRECIS_NODE lane name and
    ``reserve_host_id`` the reserve/lease-host namespace — the running-cap
    and share counts key on the latter (``meta.lease_host`` stamps), NOT
    the node gate. Tests stamp running rows ``lease_host="h1"``."""
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="job_inproc",
            limit=limit,
            node=node,
            reserve_host_id="h1",
        )
    return [int(r[0]) for r in rows]


def test_fresh_recurring_outranks_bound_background(store: Store) -> None:
    """Inside the promotion bound a fresh recurring row still wins the
    class-tie on in-class age; the spec's starvation bound (3 h) is what
    ultimately lets the background row pass — not foreground order."""
    bg = _job(store, origin="background", prio=8, queued_s_ago=1900)
    rec = _job(store, origin="recurring", prio=2)
    assert _claim(store)[:2] == [rec, bg]


def test_aged_background_passes_recurring_within_bound(store: Store) -> None:
    """D3's starvation bound: a background row old enough to have
    climbed to the top class present and aged to its floor claims ahead
    of a fresh recurring row — a bounded wait, never starvation."""
    bg = _job(store, origin="background", prio=8, queued_s_ago=7200)
    rec = _job(store, origin="recurring", prio=2)
    assert _claim(store)[:2] == [bg, rec]


def test_promoted_background_between_classes(store: Store) -> None:
    """A 31-min background row promotes past fresh background but its
    in-class age (4.9) still loses to a fresh recurring prio-2 — the
    spec's between-classes pin."""
    fresh_bg = _job(store, origin="background", prio=2)
    old_bg = _job(store, origin="background", prio=8, queued_s_ago=1860)
    rec = _job(store, origin="recurring", prio=2)
    assert _claim(store)[:3] == [rec, old_bg, fresh_bg]


def test_agent_outranks_recurring(store: Store) -> None:
    agent = _job(store, origin="agent", prio=5)
    rec = _job(store, origin="recurring", prio=2)
    assert _claim(store)[:2] == [agent, rec]


def test_prio1_preempts_across_classes(store: Store) -> None:
    human = _job(store, origin="human", prio=5)
    p1 = _job(store, origin="background", prio=1)
    assert _claim(store)[:2] == [p1, human]


def test_missing_origin_reads_background_or_cron_band(store: Store) -> None:
    """Pre-slice-1 rows: prio <= 2 reads as recurring, else background.
    The unstamped prio-8 band row climbs to tie the cron class and wins
    the age tie-break — the same bounded-starvation rule."""
    old_band = _job(store, origin=None, prio=8, queued_s_ago=7200)
    cron = _job(store, origin=None, prio=2)
    assert _claim(store)[:2] == [old_band, cron]


def test_max_running_refuses_cap_plus_one(store: Store) -> None:
    """An ``app_state`` ``claim.max_running.<job_type>`` cap bounds
    running rows on this host; the (cap+1)th claim is excluded."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.max_running.capped', '1') "
            "ON CONFLICT (key) DO UPDATE SET value = '1'"
        )
        conn.commit()
    running = _job(
        store, origin="background", prio=8, job_type="capped", status="running"
    )
    waiting = _job(store, origin="background", prio=8, job_type="capped")
    other = _job(store, origin="background", prio=8, job_type="other")
    claimed = _claim(store)
    assert claimed == [other]
    assert waiting not in claimed and running not in claimed


def test_default_cap_applies_to_derived_drain(store: Store) -> None:
    """The code-shipped default caps ``derived_drain`` running per host
    even with no ``app_state`` row — the band can't fill the lane."""
    for _ in range(_DRAIN_CAP):
        _job(
            store,
            origin="background",
            prio=8,
            job_type="derived_drain",
            status="running",
        )
    queued_drain = _job(store, origin="background", prio=8, job_type="derived_drain")
    embed = _job(store, origin="background", prio=2, job_type="embed_batch")
    assert _claim(store) == [embed]
    assert queued_drain not in _claim(store)


def test_capped_band_does_not_starve_later_rows(store: Store) -> None:
    """With derived_drain at its cap and many drains queued ahead, an
    embed_batch behind them is still claimed (SQL exclusion, not a
    skipped fetch window)."""
    for _ in range(_DRAIN_CAP):
        _job(
            store,
            origin="background",
            prio=8,
            job_type="derived_drain",
            status="running",
        )
    for _ in range(5):
        _job(store, origin="background", prio=8, job_type="derived_drain")
    embed = _job(store, origin="background", prio=8, job_type="embed_batch")
    assert embed in _claim(store)


def test_kv_override_zero_suspends_a_type(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.max_running.held', '0') "
            "ON CONFLICT (key) DO UPDATE SET value = '0'"
        )
        conn.commit()
    held = _job(store, origin="human", prio=2, job_type="held")
    free = _job(store, origin="background", prio=8, job_type="free")
    claimed = _claim(store)
    assert claimed == [free]
    assert held not in claimed


def test_cap_check_is_serialized_by_advisory_lock(
    store: Store, monkeypatch: Any
) -> None:
    """Two concurrent claimers can't both pass the cap: while one holds
    ``claim-cap:<type>``, a racing claim's ``pg_try_advisory_xact_lock``
    loses and the type reads as at-cap for that pass — the racing claim
    returns at once WITHOUT the row instead of queueing on the holder's
    open transaction until ``lock_timeout`` kills its whole pass (the
    ``test_job_claim_holds_a_real_row_lock_skip_locked`` boundary pin).
    Once the holder commits, the next pass counts fresh and claims."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.max_running.serial', '1') "
            "ON CONFLICT (key) DO UPDATE SET value = '1'"
        )
        conn.commit()
    serial = _job(store, origin="background", prio=8, job_type="serial")
    host = "h1"
    monkeypatch.setattr(
        "precis.workers.executors._common._this_worker_lease_identity",
        lambda: ("boot", "proc", host),
    )

    # Hold the cap lock open on one tx: the racing claim excludes
    # 'serial' (the only claimable row) rather than blocking on it.
    holder = store.pool.getconn()
    holder.execute("SELECT pg_advisory_xact_lock(hashtext('claim-cap:serial')::bigint)")
    assert _claim(store, limit=1) == []
    holder.commit()
    store.pool.putconn(holder)
    # Released — the next pass counts fresh and takes the row.
    assert _claim(store, limit=1) == [serial]


def test_kill_switch_restores_aged_order(store: Store, monkeypatch: Any) -> None:
    """``PRECIS_CLAIM_CLASS_RANK=0`` restores the pre-slice-2 order:
    aged prio first, no class rank — a fresh prio-2 background row
    outranks a fresh prio-9 human row again."""
    human = _job(store, origin="human", prio=9)
    bg = _job(store, origin="background", prio=2)
    assert _claim(store)[:2] == [human, bg]  # class order first
    # Re-queue both so the flag-off claim sees the same candidates.
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM ref_tags WHERE ref_id = ANY(%s)", ([human, bg],))
        conn.commit()
    store.add_tag(human, Tag.parse_strict("STATUS:queued"))
    store.add_tag(bg, Tag.parse_strict("STATUS:queued"))
    monkeypatch.setenv(CLAIM_CLASS_RANK_ENV, "0")
    assert _claim(store)[:2] == [bg, human]  # old aged order: prio wins


def test_derived_drain_run_bound_truncates_cleanly(
    store: Store, monkeypatch: Any
) -> None:
    """The 10-min run bound: the drain stops between batches, keeps its
    partial progress and exits with meta.truncated — not a kill."""
    from precis.workers.job_types import derived_drain

    ticks = iter([1000.0, 1000.0 + derived_drain._MAX_RUN_S + 1.0])
    monkeypatch.setattr(derived_drain.time, "monotonic", lambda: next(ticks))
    monkeypatch.setitem(
        derived_drain._RUNNER_FACTORIES,
        "classify",
        lambda: lambda **kw: {"claimed": 0, "ok": 0, "failed": 0},
    )

    ref = _job(store, origin="background", prio=8, job_type="derived_drain")

    class _Ctx:
        def __init__(self) -> None:
            self.store = store
            self.ref_id = ref
            self.meta: dict[str, Any] = {
                "params": {"pass": "classify", "limit": 10, "concurrency": 1}
            }
            self.chunks: list[Any] = []

        def record_failure(self, msg: str, **kw: Any) -> None:
            raise AssertionError(msg)

        def append_chunk(self, kind: str, text: str) -> None:
            self.chunks.append((kind, text))

        def set_meta(self, **fields: Any) -> None:
            self.meta.update(fields)
            from precis.workers.executors._common import set_meta

            with self.store.pool.connection() as conn:
                set_meta(conn, self.ref_id, **fields)
                conn.commit()

    ctx = _Ctx()
    derived_drain._dispatch(cast(Any, ctx), derived_drain.SPEC)
    with store.pool.connection() as conn:
        row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (ref,)).fetchone()
    assert row is not None
    assert row[0]["truncated"] is True  # persisted to the DB row
    assert row[0]["truncated_remaining"] == 10


def test_aged_background_backlog_never_blocks_human(store: Store) -> None:
    """Review round 3: a background backlog aged past 90 min promotes to
    at most the recurring rank — it can TIE the top class queued but
    never reaches agent/human, so the LIMIT*overfetch window can't fill
    with promoted rows ahead of a fresh human row."""
    for _ in range(6):
        _job(store, origin="background", prio=8, queued_s_ago=5500)
    human = _job(store, origin="human", prio=5)
    assert _claim(store, limit=1) == [human]


def test_in_call_cap_counts_claims(store: Store) -> None:
    """One claim call with limit>cap can't over-claim: the per-row
    running count increments inside the loop."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.max_running.twice', '2') "
            "ON CONFLICT (key) DO UPDATE SET value = '2'"
        )
        conn.commit()
    for _ in range(5):
        _job(store, origin="background", prio=8, job_type="twice")
    claimed = _claim(store, limit=5)
    assert len(claimed) == 2


# ── D4 share half: round-robin by (job_type, params.pass) share ────────


def _set_drain_cap(store: Store, cap: int) -> None:
    """Override the ``derived_drain`` running cap so a share test has room
    for two bands — the code default (1) deliberately leaves one slot free
    for control jobs (gr646119), leaving no second drain slot to share."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.max_running.derived_drain', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(cap),),
        )
        conn.commit()


def test_share_prefers_band_with_fewest_running(store: Store) -> None:
    """gr506754: a full summarize band sat 0-running for hours behind the
    classify band's rows — both bands share job_type ``derived_drain``, so
    FIFO-by-age let one band hold the capped lane. The share term
    (fewest running per ``job_type``+``params.pass`` on this host) lets
    the starved band claim the next free slot."""
    _set_drain_cap(store, 4)
    _job(
        store,
        origin="background",
        prio=8,
        job_type="derived_drain",
        status="running",
        params={"pass": "classify"},
    )
    older_cls = _job(
        store,
        origin="background",
        prio=8,
        queued_s_ago=7200,
        job_type="derived_drain",
        params={"pass": "classify"},
    )
    summ = _job(
        store,
        origin="background",
        prio=8,
        job_type="derived_drain",
        params={"pass": "llm_summarize"},
    )
    # summarize's share has 0 running vs classify's 1 — it claims the free
    # slot ahead of the much-older classify row.
    assert _claim(store, limit=1) == [summ]
    assert isinstance(older_cls, int)


def test_share_round_robins_within_one_claim(store: Store) -> None:
    """One multi-claim call interleaves the shares rather than draining
    the oldest band's prefix: pick order goes oldest → other share →
    oldest — not oldest, second-oldest, third-oldest."""
    _set_drain_cap(store, 4)
    cls1 = _job(
        store,
        origin="background",
        prio=8,
        queued_s_ago=7200,
        job_type="derived_drain",
        params={"pass": "classify"},
    )
    _job(
        store,
        origin="background",
        prio=8,
        queued_s_ago=7100,
        job_type="derived_drain",
        params={"pass": "classify"},
    )
    summ = _job(
        store,
        origin="background",
        prio=8,
        job_type="derived_drain",
        params={"pass": "llm_summarize"},
    )
    # Pick 1: shares tie at 0 running → aged order → the older classify row.
    # Pick 2: classify's share now has 1 running → summarize wins despite age.
    assert _claim(store, limit=2) == [cls1, summ]


def test_share_ignores_types_without_pass(store: Store) -> None:
    """The same fewest-running share rule at plain job_type granularity:
    a type with running rows yields the next claim to a type with none —
    the ``params.pass`` discriminator only refines the key when present."""
    _job(store, origin="background", prio=8, job_type="embed_batch", status="running")
    old_other = _job(
        store, origin="background", prio=8, queued_s_ago=7200, job_type="other"
    )
    _job(store, origin="background", prio=8, job_type="embed_batch")
    # other's share has 0 running, embed_batch has 1 → the share term
    # outranks aged order.
    assert _claim(store, limit=1) == [old_other]


def test_share_term_off_under_kill_switch(store: Store, monkeypatch: Any) -> None:
    """``PRECIS_CLAIM_CLASS_RANK=0`` drops the share term with the class
    order — the aged order claims the oldest row regardless of which
    band holds the running slots."""
    _set_drain_cap(store, 4)
    _job(
        store,
        origin="background",
        prio=8,
        job_type="derived_drain",
        status="running",
        params={"pass": "classify"},
    )
    older_cls = _job(
        store,
        origin="background",
        prio=8,
        queued_s_ago=7200,
        job_type="derived_drain",
        params={"pass": "classify"},
    )
    _job(
        store,
        origin="background",
        prio=8,
        job_type="derived_drain",
        params={"pass": "llm_summarize"},
    )
    monkeypatch.setenv(CLAIM_CLASS_RANK_ENV, "0")
    assert _claim(store, limit=1) == [older_cls]


def test_yielded_is_claimable(store: Store) -> None:
    """relax-slices: ``STATUS:yielded`` is a claimable queue member —
    a released slice is taken like a queued row, not parked."""
    yielded = _job(store, origin="background", prio=8, status="yielded")
    assert _claim(store) == [yielded]


def test_yielded_requeues_behind_waiters(store: Store) -> None:
    """The decided queue-age rule, pinned: a yield re-stamps the status
    tag, so the row lands behind jobs already waiting — the accumulated
    age restarts (it does NOT keep the pre-claim stamp)."""
    older = _job(store, origin="background", prio=8, queued_s_ago=60)
    yielded = _job(store, origin="background", prio=8, status="yielded")
    assert _claim(store)[:2] == [older, yielded]


def test_yielded_keeps_class_human_first(store: Store) -> None:
    """A yielded background row re-queues at its ORIGINAL prio/origin —
    a fresh human-origin job still claims first after the yield
    (Reto 2026-10-22 human precedence)."""
    yielded = _job(store, origin="background", prio=5, status="yielded")
    human = _job(store, origin="human", prio=5)
    assert _claim(store)[:2] == [human, yielded]


# ── gr592211: drains can never occupy every in-proc slot ──────────────


def test_control_job_claims_next_pass_at_drain_cap(store: Store) -> None:
    """gr592211: with the derived_drain cap full and a
    50-deep drain backlog queued, a recurring ``release_poll`` claims on
    the very next pass — the last slot never fills with drains."""
    for _ in range(_DRAIN_CAP):
        _job(
            store,
            origin="background",
            prio=8,
            job_type="derived_drain",
            status="running",
        )
    poll = _job(
        store, origin="recurring", prio=2, job_type="release_poll", status="queued"
    )
    for _ in range(50):
        _job(store, origin="background", prio=8, job_type="derived_drain")
    assert _claim(store, limit=10) == [poll]


def test_cap_binds_by_lease_host_not_claim_node(store: Store) -> None:
    """gr592211 regression pin: the running count keys on
    ``meta.lease_host`` (the ``reserve_host()`` stamp), NOT ``node``
    (PRECIS_NODE — a lane name, not a machine). Where the two diverge a
    node-keyed count reads 0 running rows and the cap never binds."""
    for _ in range(_DRAIN_CAP):
        _job(
            store,
            origin="background",
            prio=8,
            job_type="derived_drain",
            status="running",  # stamped lease_host="h1" by _job
        )
    poll = _job(store, origin="recurring", prio=2, job_type="release_poll")
    for _ in range(5):
        _job(store, origin="background", prio=8, job_type="derived_drain")
    # node="gate" names the claim lane; the host is h1 via reserve_host_id.
    assert _claim(store, limit=10, node="gate") == [poll]


def test_uncapped_jobs_claim_through_starved_lane(store: Store) -> None:
    """The cap excludes only the capped type: embed_batch (no cap) still
    claims while derived_drain sits at its cap — the free slot goes to
    whatever ranks first, drains wait."""
    for _ in range(_DRAIN_CAP):
        _job(
            store,
            origin="background",
            prio=8,
            job_type="derived_drain",
            status="running",
        )
    for _ in range(3):
        _job(store, origin="background", prio=8, job_type="derived_drain")
    embed = _job(store, origin="background", prio=8, job_type="embed_batch")
    assert _claim(store, limit=10) == [embed]


def test_drain_cap_leaves_a_lane_free_of_the_deployed_drain_lanes() -> None:
    """gr646119: the ``derived_drain`` cap must stay strictly below the
    dedicated drain-lane count the playbook deploys by default — a cap
    equal to the lane count (it was 2 of 2) lets drains hold every
    ``job_inproc`` slot and release-class jobs queue behind them for
    tens of minutes, which class ranking cannot fix."""
    playbook = (
        Path(__file__).resolve().parents[2]
        / "deploy/playbooks/20d-precis-worker-drain.yml"
    )
    match = re.search(
        r"^\s*precis_worker_drain_lanes:\s*(\d+)\s*$",
        playbook.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "precis_worker_drain_lanes default not found in 20d playbook"
    lanes = int(match.group(1))
    assert lanes >= 2
    assert 1 <= _DRAIN_CAP < lanes


# ── gr633506 (revised scope): the background-class share ────────────────
#
# Class promotion ties a background row at the recurring rank, so a
# continuously queued agent/human front holds the whole class down
# indefinitely. About 1 fresh claim in ``DEFAULT_BG_SHARE_EVERY``
# (``PRECIS_CLAIM_BG_SHARE_EVERY``) diverts to the oldest waiting
# background row; human-class, prio-1 and release-band winners never
# divert, and an empty background candidate set leaves the pick alone.


_BG_SHARE_EVERY_ENV = "PRECIS_CLAIM_BG_SHARE_EVERY"


def test_background_share_diverts_tenth_claim(store: Store) -> None:
    """The default cadence: 9 agent claims, then the 10th diverts to the
    waiting background row even though agents are still queued."""
    agents = [_job(store, origin="agent", prio=5) for _ in range(12)]
    bg = _job(store, origin="background", prio=8, queued_s_ago=3600)
    claimed = _claim(store, limit=11)
    assert claimed[:9] == agents[:9]
    assert claimed[9] == bg  # the 10th claim is the diversion
    assert claimed[10] == agents[9]


def test_background_share_counter_persists_across_passes(store: Store) -> None:
    """The cadence counter is fleet-wide, not per pass: 6 fresh claims in
    pass one + 4 in pass two put the diversion mid-batch of pass two."""
    agents = [_job(store, origin="agent", prio=5) for _ in range(10)]
    bg = _job(store, origin="background", prio=8, queued_s_ago=3600)
    first = _claim(store, limit=6)
    assert first == agents[:6]
    # Claimed rows leave the queue — the second pass sees only the rest
    # (the test harness leaves STATUS queued, so flip them to running).
    for ref_id in first:
        store.add_tag(ref_id, Tag.parse_strict("STATUS:running"), replace_prefix=True)
    second = _claim(store, limit=5)
    assert second[:3] == agents[6:9]
    assert second[3] == bg  # global claim 10
    assert second[4] == agents[9]


def test_background_share_diverts_recurring_winner(
    store: Store, monkeypatch: Any
) -> None:
    """Recurring-class winners forgo the share too — any winner above the
    background class can be diverted, not just agents."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "2")
    rec = [_job(store, origin="recurring", prio=2) for _ in range(3)]
    bg = _job(store, origin="background", prio=8)
    # Claims 1 and 3: recurring; claim 2 diverts to background.
    assert _claim(store, limit=3) == [rec[0], bg, rec[1]]


def test_background_share_human_class_still_preempts(
    store: Store, monkeypatch: Any
) -> None:
    """A human-class winner is never diverted: with the share forced on
    for EVERY claim, the human row still claims first and only the
    next (agent) winner yields to the background row."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "1")
    human = _job(store, origin="human", prio=5)
    bg = _job(store, origin="background", prio=8)
    agent = _job(store, origin="agent", prio=5)
    assert _claim(store, limit=3) == [human, bg, agent]


def test_background_share_prio1_winner_never_diverts(
    store: Store, monkeypatch: Any
) -> None:
    """The prio-1 preempt band stays absolute: an agent-class prio-1 row
    (otherwise a divertible winner) claims ahead of the share."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "1")
    p1 = _job(store, origin="agent", prio=1)
    bg = _job(store, origin="background", prio=8, queued_s_ago=600)
    agent = _job(store, origin="agent", prio=5)
    assert _claim(store, limit=3) == [p1, bg, agent]


def test_background_share_respects_release_band(store: Store, monkeypatch: Any) -> None:
    """A release-leg winner is not diverted — gr584136's freeze protects
    the band from aged lower classes and from the share alike."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "1")
    poll = _job(store, origin="recurring", prio=2, job_type="release_poll")
    bg = _job(store, origin="background", prio=8, queued_s_ago=7200)
    assert _claim(store, limit=2) == [poll, bg]


def test_background_share_drains_background_freely(
    store: Store, monkeypatch: Any
) -> None:
    """Spare capacity: with only background rows queued the diversion
    test never fires (winner base 3) — natural aged order stands."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "1")
    old_bg = _job(store, origin="background", prio=8, queued_s_ago=60)
    new_bg = _job(store, origin="background", prio=8)
    assert _claim(store, limit=2) == [old_bg, new_bg]


def test_background_share_env_zero_disables(store: Store, monkeypatch: Any) -> None:
    """``PRECIS_CLAIM_BG_SHARE_EVERY=0`` turns the share off entirely."""
    monkeypatch.setenv(_BG_SHARE_EVERY_ENV, "0")
    agents = [_job(store, origin="agent", prio=5) for _ in range(11)]
    bg = _job(store, origin="background", prio=8)
    assert _claim(store, limit=11) == agents
    assert bg not in agents
