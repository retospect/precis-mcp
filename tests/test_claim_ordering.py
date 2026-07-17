"""Claim ordering — prio DESC, then age (slice 6a).

``claim_executor_jobs`` was pure FIFO (``ORDER BY ref_id``). 6a makes it
``ORDER BY COALESCE(prio, 5) DESC, ref_id ASC``: a high-prio job (prio
flows down the DAG from its parent todo) claims ahead of commodity work,
oldest-first breaks ties within a prio band, and an all-unset queue stays
FIFO. A synthetic executor name isolates each test's queue from any other
jobs sharing the test DB.
"""

from __future__ import annotations

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import claim_executor_jobs


def _queue_job(store: Store, *, executor: str, prio: int | None) -> int:
    """Insert a ``STATUS:queued`` job for ``executor`` with the given prio."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"job prio={prio}",
        meta={"job_type": "demo", "executor": executor, "params": {}},
        prio=prio,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    return ref.id


def _claim_order(store: Store, executor: str) -> list[int]:
    """The ref_ids the claim would lock, in order (rolled back — no side effects)."""
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(conn, executor=executor, limit=50)
        conn.rollback()
    return [r[0] for r in rows]


def test_higher_prio_claimed_first(store: Store) -> None:
    ex = "test_order_prio_first"
    low = _queue_job(store, executor=ex, prio=2)
    high = _queue_job(store, executor=ex, prio=9)
    mid = _queue_job(store, executor=ex, prio=5)
    # Insertion order low→high→mid; claim order must be by prio DESC.
    assert _claim_order(store, ex) == [high, mid, low]


def test_same_prio_orders_by_age(store: Store) -> None:
    ex = "test_order_same_prio"
    first = _queue_job(store, executor=ex, prio=5)
    second = _queue_job(store, executor=ex, prio=5)
    third = _queue_job(store, executor=ex, prio=5)
    # Equal prio → oldest (smallest ref_id) first: the pre-6a FIFO tiebreak.
    assert _claim_order(store, ex) == [first, second, third]


def test_unset_prio_is_fifo(store: Store) -> None:
    ex = "test_order_unset"
    a = _queue_job(store, executor=ex, prio=None)
    b = _queue_job(store, executor=ex, prio=None)
    # All NULL → COALESCE default for everyone → age order (FIFO).
    assert _claim_order(store, ex) == [a, b]


def test_null_prio_ranks_as_default_midpoint(store: Store) -> None:
    ex = "test_order_null_midpoint"
    top = _queue_job(store, executor=ex, prio=8)
    unset = _queue_job(store, executor=ex, prio=None)  # ranks as 5
    bottom = _queue_job(store, executor=ex, prio=3)
    # 8 > 5(default) > 3 — the unset job slots into the middle band.
    assert _claim_order(store, ex) == [top, unset, bottom]
