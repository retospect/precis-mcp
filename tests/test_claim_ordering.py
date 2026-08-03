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
