"""Claim ageing — an old low-priority job is not starved by fresh urgent work.

Repro for ``docs/backlog/claude-inproc-lane-starves-old-jobs.md``: the
claim ordered strictly by ``prio`` then age, so a steady stream of fresh
prio-2 cron work (``news_poll``) kept prio-5/8 jobs (``plan_tick``,
``draft_refresh``, ``diagnose_gripe``) queued for hours on a live lane.
Prod, last 30 days of claude_inproc (``meta.started_at - created_at``):
prio-8 ``diagnose_gripe`` / ``doctor_tick`` / ``draft_refresh`` waited up
to 186 / 185 / 164 min while 1217 prio-2 ``news_poll`` jobs ran at a
median wait of 0.

Deterministic simulated clock and queue against the real claim SQL: each
5-minute tick one fresh prio-2 job arrives and the lane claims one job, so
fresh work alone saturates it. Every queued row's ``STATUS:queued`` tag is
re-stamped to ``now() - simulated age`` before each claim, so the claim
sees exactly the simulated waits.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import (
    _CLASS_RANK_CASE,
    _claim_order_sql,
    claim_executor_jobs,
    set_status,
)

_TICK_S = 300
#: ``_common.DEFAULT_CLAIM_AGE_STEP_S`` — literal so the repro runs on main.
_AGE_STEP_S = 600
_CLASS_AGE_S = 1800
_HORIZON_TICKS = 12 * 3600 // _TICK_S


def _queue_job(
    store: Store, executor: str, prio: int, origin: str | None = None
) -> int:
    meta: dict[str, Any] = {"job_type": "demo", "executor": executor, "params": {}}
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
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return ref.id


def _stamp_queued_since(store: Store, ages_s: dict[int, int]) -> None:
    """Back-date each queued row's ``STATUS:queued`` tag to its simulated age."""
    with store.pool.connection() as conn:
        for ref_id, age_s in ages_s.items():
            conn.execute(
                """
                UPDATE ref_tags rt SET created_at = now() - %s
                  FROM tags t
                 WHERE t.tag_id = rt.tag_id AND rt.ref_id = %s
                   AND t.namespace = 'STATUS' AND t.value = 'queued'
                """,
                (timedelta(seconds=age_s), ref_id),
            )
        conn.commit()


def _simulate(store: Store, executor: str, old_prios: list[int]) -> dict[int, int]:
    """Run the saturated lane; return ``{prio: wait_s}`` for one job per
    ``old_prios`` entry queued at t=0. An unclaimed job waits the horizon."""
    prio_of = {_queue_job(store, executor, prio): prio for prio in old_prios}
    arrivals: dict[int, int] = dict.fromkeys(prio_of, 0)
    waits: dict[int, int] = {}
    for tick in range(_HORIZON_TICKS):
        now_s = tick * _TICK_S
        # The flood is stamped ``recurring`` — the class ABOVE the
        # unstamped (background) old rows — because slice 2 (D3) makes
        # ageing class-preserving and the class-step bound is what the
        # wait budget now rests on.
        arrivals[_queue_job(store, executor, 2, origin="recurring")] = now_s
        _stamp_queued_since(store, {rid: now_s - at for rid, at in arrivals.items()})
        with store.pool.connection() as conn:
            rows = claim_executor_jobs(conn, executor=executor, limit=1)
            conn.commit()
        for ref_id, _title, _meta in rows:
            set_status(store, ref_id, "succeeded")
            at = arrivals.pop(ref_id)
            if ref_id in prio_of:
                waits[prio_of[ref_id]] = now_s - at
        if len(waits) == len(prio_of):
            break
    horizon_s = _HORIZON_TICKS * _TICK_S
    return {prio: waits.get(prio, horizon_s) for prio in prio_of.values()}


def test_old_low_prio_jobs_claimed_within_bound_under_fresh_flood(
    store: Store,
) -> None:
    """A prio-p job outranks every fresh prio-2 arrival once it has waited
    ``(p - 2)`` age steps. Its wait is bounded by that plus draining what
    already ranks ahead of it: each other old job claimed first leaves one
    more prio-2 job in the backlog, one tick each."""
    waits = _simulate(store, "test_claim_ageing_flood", [5, 8])
    for prio, wait_s in waits.items():
        bound_s = (prio - 2) * _AGE_STEP_S + len(waits) * _TICK_S
        assert wait_s <= bound_s, (
            f"prio-{prio} job waited {wait_s // 60} min under a prio-2 flood; "
            f"bound {bound_s // 60} min (all waits, min: "
            f"{ {p: w // 60 for p, w in waits.items()} })"
        )


def test_ageing_keeps_prio_order_among_fresh_jobs(store: Store) -> None:
    """Jobs queued moments apart still claim strictly by prio: ageing only
    moves a job one band per age step."""
    ex = "test_claim_ageing_fresh"
    urgent = _queue_job(store, ex, 1)
    default = _queue_job(store, ex, 5)
    commodity = _queue_job(store, ex, 8)
    _stamp_queued_since(store, {urgent: 0, default: 60, commodity: 120})
    with store.pool.connection() as conn:
        order = [r[0] for r in claim_executor_jobs(conn, executor=ex, limit=10)]
        conn.rollback()
    assert order == [urgent, default, commodity]


def test_null_queued_since_ranks_at_unaged_prio(store: Store) -> None:
    """A NULL ``queued_since`` (re-tag race) ages as zero: the row keeps
    its own prio and base class; a 30-min-old row promotes one class and
    ages three bands toward the in-class floor (D3)."""
    rank_sql, aged_sql, params = _claim_order_sql(_AGE_STEP_S, _CLASS_AGE_S)

    def _at(qs_expr: str):
        with store.pool.connection() as conn:
            return conn.execute(
                f"SELECT {rank_sql}, {aged_sql} "
                "FROM (VALUES (8, NULL::jsonb)) AS r(prio, meta) "
                f"CROSS JOIN LATERAL (SELECT {qs_expr} AS qs) q "
                "CROSS JOIN LATERAL (SELECT COALESCE("
                "EXTRACT(EPOCH FROM (now() - q.qs))::float8, 0) AS age_s, "
                f"({_CLASS_RANK_CASE}) AS base_class) b "
                # A recurring row queued alongside: the promotion clamp
                # reads the top non-prio-1 class present.
                "CROSS JOIN (VALUES (2)) AS t(top_class)",
                (*params, 5),
            ).fetchone()

    row = _at("NULL::timestamptz")
    old = _at("now() - interval '30 minutes'")
    assert row is not None and old is not None
    assert row[0] == pytest.approx(3.0)  # missing origin, prio>2 → background
    assert row[1] == pytest.approx(8.0)
    assert old[0] == pytest.approx(2.0)  # promoted one class at the bound
    assert old[1] == pytest.approx(5.0)  # 30 min = 3 steps of 600 s
